---
applyTo: '**/input_GUI_w_fixed_autoclick_closed_loop.pyw'
---

# Hims & Hers EMR Site Styling Instructions

Use this file when making visual or CSS-related changes to the live Hims & Hers EMR page via the JS overlay system.

## Scope and Ownership

- This file applies to styling changes for the EMR web page itself, not the wxPython desktop window.
- Site styling is currently injected from the Python app into the live page through Playwright/CDP.
- The overlay and page styling only apply in JS overlay mode.
- The Python window and the injected page UI are separate systems. Do not confuse desktop UI theme work with EMR page theme work.

## Hard Architecture Constraint

- Only the overlay thread may call `page.evaluate()` or any Playwright DOM/CSS mutation API.
- Never mutate page DOM or inject CSS from the wx thread, grab threads, or monitor threads.
- All styling updates must flow through the overlay command queue and the overlay-owned evaluation path.
- For dark mode or page theme changes, update the injected CSS inside the overlay-managed dark-mode application logic rather than creating ad hoc DOM mutations in unrelated code paths.

## Styling Strategy

- Prefer targeted surface styling over broad global inversion or filter hacks.
- Do not rely on `filter: invert(...)` as the main approach. It is acceptable only as a quick probe, not as a durable solution.
- Style the actual containers, cards, tabs, popups, inputs, text areas, pickers, and modal shells.
- Expect to use `!important` frequently because the site mixes bundled CSS, inline styles, utility classes, and runtime-generated classes.

## What The Native Site Looks Like

The clean native style dump showed the following:

- The site is a single same-origin document in the captured state, not an iframe-heavy app.
- Styling comes from a mix of:
  - Next.js bundled CSS files
  - multiple inline `<style>` blocks
  - utility classes
  - runtime-generated `css-*` and `r-*` class combinations
  - inline `style=""` attributes on many interactive surfaces
- There is no single clean theme-variable layer that controls the whole site.

## Native Visual Baseline

The native site is light-themed by default.

Frequently recurring native values seen in the dump:

- Main light surface backgrounds often use `rgb(248, 248, 248)`.
- Light neutral borders often use `rgb(244, 244, 244)`.
- Default dark text frequently resolves to `oklch(0.21 0.006 285.885)`.
- Some placeholder or muted text uses values around `#b2b2b2`.
- Some input surfaces use `Sofia Pro` via inline style.

Do not assume these are centralized theme tokens. They often appear as computed values after several layers of CSS and inline style resolution.

## Selector Priority

When styling this site, prefer selectors in this order:

1. `data-testid`
2. semantic attributes like `role`, `aria-label`, `placeholder`, or stable ids
3. short class combinations using meaningful utility classes
4. combined runtime class fragments like `css-*` and `r-*`
5. inline-style substring selectors only when nothing more stable exists

Examples of strong native anchors seen in the clean dump:

- `patientSummarySidebar`
- `healthOverview`
- `managedConditions`
- `tab-messages`
- `tab-notes`
- `searchInputField`
- `search-icon`
- `settingsMenu`
- `Type a message...`
- `floatingActionButton`
- `continueRxButton`
- `referPatientButton`
- `proposedTreatmentPlan`
- `treatmentPlan`
- `treatment-0`
- `medication-title`
- `medication-text`

Additional stable anchors discovered during styling work:

- `searchPickerInput`
- `searchPickerInput-dropdown`
- `Edit Prescription Panel`
- `closeModalButton`
- `diagnosisSelector`
- `soapNote`
- `messageToPatient`
- `medicationDirections`
- `cancelPrescriptionButton`
- `savePrescriptionButton`
- `Change-duration-button`
- `menu-item-*`

## Major Native Component Families

Treat the site as a set of component families. Style by family rather than by random one-off selector whenever possible.

### 1. Top header / command bar

Common traits:

- `h-[74px]`
- `bg-surface-primary`
- search input
- settings menu
- task buttons
- badges

Behavior learned in practice:

- The header needed its own dedicated gradient/surface treatment.
- Header child icons, text, and controls needed explicit recoloring.
- Search field shells and nested input elements must both be styled.

### 2. Patient sidebar and health overview cards

Common anchors:

- `patientSummarySidebar`
- `healthOverview`
- `health-overview-section-*`
- `managedConditions`
- `rx-*`
- `erx-*`

Behavior learned in practice:

- The sidebar root can be transparent while its children or descendants own the visible card backgrounds.
- The sidebar, managed conditions, and health overview sections responded best to targeted card-level styling.
- Text weights and muted labels often required explicit color correction.

### 3. Tabs, note/message panes, and composer

Common anchors:

- `tab-messages`
- `tab-notes`
- `Type a message...`
- `expandMessageComposeButton`
- composer-related wrapper classes
- `searchPickerInput-dropdown`
- `menu-item-*`

Behavior learned in practice:

- Tabs frequently carry inline light backgrounds themselves.
- Composer shells, buttons, and send controls often require separate selectors.
- Dropdowns often have both an outer absolute-positioned shell and one or more inner wrappers that must be styled separately.
- Scrollbar darkening must happen on actual scrolling containers, not just the global page scrollbar.

### 4. Modals, pickers, and action menus

Common anchors:

- `Edit Prescription Panel`
- picker controls ending in `-picker`
- `closeModalButton`
- `Change-duration-button`
- `cancelPrescriptionButton`
- `savePrescriptionButton`
- `diagnosisSelector`
- `soapNote`
- `messageToPatient`
- `medicationDirections`
- `menu-item-*`

Behavior learned in practice:

- Modals often require styling of:
  - outer modal shell
  - immediate panel body
  - field rows
  - inner picker value area
  - chevrons/icons
  - text area
  - checkbox region
  - footer buttons
- White popup menus often come from inline-style absolute-positioned containers plus nested wrappers.
- One visible light area may require 2 or 3 selectors because the outer shell, the padded wrapper, and the row items each own part of the final appearance.

## Critical Structural Rule: Background Ownership Is Often On A Parent

- Do not assume the visually wrong element owns its own background.
- This site frequently uses transparent child elements, selected-state wrappers with inline backgrounds, and popup shells that own shadow and padding while descendants own text and rows.
- Always inspect the ancestor chain before deciding what to style.
- If an element still looks wrong after styling it directly, inspect:
  - parent background
  - parent border
  - parent box shadow
  - sibling selected-state wrappers
  - outer popup shells
  - inline `background-color` on wrapper nodes

## Runtime Class Guidance

The site uses runtime classes such as `css-*` and `r-*`.

Rules:

- Prefer combinations of multiple `r-*` fragments over a single fragment.
- Combine runtime classes with a stable context anchor whenever possible.
- If a runtime class selector is necessary, anchor it beneath a stronger parent such as a `data-testid`.
- Do not build brittle full-length selectors unless no better anchor exists.

Good pattern:

- `html.emr-assist-dark-page body > :not(#emr-assist-overlay) [data-testid="Edit Prescription Panel"] [class*="r-14lw9ot"][class*="r-jdbj7n"]...`

Bad pattern:

- a long unscoped document path that depends on exact tree position only

## Inline Style Guidance

Inline style is common in this app.

Use inline-style substring selectors only when needed, especially for:

- popup shells with inline `background-color`
- absolute-positioned menus
- selected tabs with inline light background
- placeholder and font styling on textareas/inputs

When using inline-style selectors:

- make them as narrow as possible
- combine with context when possible
- treat them as last-resort selectors, not first-choice selectors

## Overlay Scoping Rule

All site-targeting CSS should be scoped away from the overlay itself.

Use the site-targeting prefix pattern:

- `html.emr-assist-dark-page body > :not(#emr-assist-overlay) ...`

This avoids recoloring the overlay toolbar and prevents site rules from bleeding into overlay controls.

## Dark Mode Implementation Guidance

Use a semantic page palette defined in the injected stylesheet, then map native surfaces onto those variables.

Recommended pattern:

- page/root background
- primary panel surface
- secondary card/input surface
- elevated button/pill surface
- border
- strong border
- primary text
- soft text
- muted text
- accent
- scrollbar track/thumb
- warm/advisory surfaces when needed

Do not mirror the site’s native light values. The point is to remap surfaces intentionally, not to preserve light-theme hierarchy exactly.

## What Worked Best In This Session

- use `data-testid` selectors first
- style real surfaces instead of global inversion
- inspect ancestor ownership for every stubborn light patch
- style popups at multiple layers when needed
- handle text, icons, borders, and shadows explicitly, not just backgrounds
- restyle scrollbars on the specific scrolling containers
- keep selectors grouped by component family
- make incremental, local changes and verify quickly

## What Did Not Work Well

- page-wide invert/filter dark mode
- relying only on global scrollbar styling
- assuming the directly clicked element owns the visible background
- assuming a stable utility class exists when a `data-testid` is available
- broad unscoped class-only rules that catch unrelated UI

## Console / Inspection Workflow

Before adding or changing selectors:

1. Inspect the target element and its ancestors.
2. Identify the actual background owner.
3. Check whether a `data-testid` exists.
4. If not, try `role`, `aria-label`, `placeholder`, or a stable id.
5. If still nothing stable exists, use a narrow combination of runtime classes.
6. If the visible light patch is from a popup or modal, inspect both outer and inner shells.
7. Verify whether inline styles are overriding the bundle CSS.

When using a style dump:

- trust `nativeElements` and `nativeStyleSheets` from clean captures
- ignore overlay-specific output
- use the ancestor chain to identify ownership of background, border, and shadow
- use selector candidates to prefer the most stable target

## Verification Workflow

After any styling change:

1. Verify the affected surface in the live EMR page.
2. Check neighboring surfaces in the same component family.
3. Confirm text, icons, borders, and shadows are coherent.
4. Confirm no overlay UI was accidentally restyled.
5. If working in Python code, run a narrow syntax/compile validation after edits.