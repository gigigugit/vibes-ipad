# Keyboard Shortcuts

This file inventories the keyboard shortcuts defined in this workspace, grouped by shortcut key so overlaps are easy to spot.

## Current wx App: Grouped By Shortcut

| Shortcut | Scope | Target | Action | Notes |
|---|---|---|---|---|
| F4 | Global | Active visit tab in main wx app | Dispatches the appropriate grab for the currently selected tab | Implemented via global keyboard hook |
| F4 | Tab-focused accelerator | Birth Control tab | Grab Birth Control data | Defined on Birth Control tab accelerator table |
| F4 | JS overlay document handler | Overlay grab button | Triggers overlay grab | Active in JS overlay mode |
| Ctrl+Alt+Enter | Global | Quick Next Task | Runs quick next task | Defined but currently disabled by config |
| Ctrl+Shift+Enter | Global Playwright click path | Floating menu then Get next task | Runs the selector-driven Playwright click path loaded from `playwright_click_paths.json` | Suppressed via keyboard hook |
| Ctrl+2 | Global Playwright click path | Floating menu then Get next task | Runs the selector-driven Playwright click path loaded from `playwright_click_paths.json` | Suppressed via keyboard hook |
| Ctrl+Alt+T | Global | Template popup | Opens template popup at mouse position | Uses keyboard module |
| Ctrl+Alt+I | Global | JS overlay notepad or in-visit autoclicker | Toggles overlay notepad when overlay is active; otherwise toggles in-visit autoclicker | Shared behavior depends on overlay state |
| Ctrl+Alt+H | Global | JS overlay | Toggle overlay minimize / restore | Only does anything when overlay is active |
| Ctrl+Alt+H | Global OS-level / fallback keyboard hook | Main GUI window | Toggle GUI visibility | Same key combo as overlay minimize toggle |
| Ctrl+Alt+F | Dynamic global when Hair Loss tab is selected | Hair Loss tab | Insert Hair follow-up note | Context-sensitive: active only while Hair Loss tab is selected |
| Ctrl+Alt+F | Dynamic global when Sexual Health tab is selected | Sexual Health tab | Insert Sexual Health follow-up note | Context-sensitive: active only while Sexual Health tab is selected |
| Ctrl+Alt+F | Dynamic global when Birth Control tab is selected | Birth Control tab | Insert Birth Control follow-up note | Context-sensitive: active only while Birth Control tab is selected |
| Ctrl+Alt+F | Tab-focused accelerator | Sexual Health tab | Insert follow-up note | Works when Sexual Health tab has focus |
| Ctrl+Alt+F | Tab-focused accelerator | Birth Control tab | Insert follow-up note | Works when Birth Control tab has focus |
| Ctrl+Alt+F | JS overlay document handler | Overlay template dropdown | Insert first template in dropdown | Overlay mode |
| Ctrl+Alt+N | Dynamic global when Hair Loss tab is selected | Hair Loss tab | Insert Hair initial note | Context-sensitive: active only while Hair Loss tab is selected |
| Ctrl+Alt+N | Dynamic global when Sexual Health tab is selected | Sexual Health tab | Insert Sexual Health plan note | Context-sensitive: active only while Sexual Health tab is selected |
| Ctrl+Alt+N | Dynamic global when Birth Control tab is selected | Birth Control tab | Insert Birth Control initial note | Context-sensitive: active only while Birth Control tab is selected |
| Ctrl+Alt+N | Tab-focused accelerator | Sexual Health tab | Insert plan note | Works when Sexual Health tab has focus |
| Ctrl+Alt+N | Tab-focused accelerator | Birth Control tab | Insert initial note | Works when Birth Control tab has focus |
| Ctrl+Alt+N | JS overlay document handler | Overlay template dropdown | Insert second template in dropdown | Overlay mode |
| Ctrl+Alt+C | Dynamic global when Hair Loss tab is selected | Hair Loss tab | Insert Hair limited check-in note | Context-sensitive: active only while Hair Loss tab is selected |
| Ctrl+Alt+C | Dynamic global when Sexual Health tab is selected | Sexual Health tab | Insert Sexual Health change template | Context-sensitive: active only while Sexual Health tab is selected |
| Ctrl+Alt+C | Tab-focused accelerator | Sexual Health tab | Insert change template | Works when Sexual Health tab has focus |
| Ctrl+Alt+C | JS overlay document handler | Overlay template dropdown | Insert third template in dropdown | Overlay mode |
| Ctrl+Alt+K | Tab-focused accelerator | Sexual Health tab | Insert Hair Info | Only defined on Sexual Health tab accelerator table |
| Ctrl+Shift+R | Global | Page refresh controller | Toggle page refresh | Uses keyboard module |
| Ctrl+Shift+D | Global | JS overlay template context menu | Toggle overlay template context menu at current mouse position | Overlay-aware behavior |

## Current wx App: Overlap Summary

| Shortcut | Overlap Type | Affected Targets |
|---|---|---|
| F4 | Reused across global, tab, and overlay paths | Global grab dispatch, Birth Control tab grab accelerator, overlay grab |
| Ctrl+Alt+H | Exact conflict | Overlay minimize toggle and GUI visibility toggle |
| Ctrl+Alt+F | Context-sensitive reuse | Hair follow-up, Sexual Health follow-up, Birth Control follow-up, overlay first template |
| Ctrl+Alt+N | Context-sensitive reuse | Hair initial note, Sexual Health plan note, Birth Control initial note, overlay second template |
| Ctrl+Alt+C | Context-sensitive reuse | Hair limited note, Sexual Health change template, overlay third template |
| Ctrl+2 | Potential reuse with disabled window-local shortcut | Active global Playwright click path in wx app; disabled AI Assist Output 2 shortcut in alternate window |

## Disabled Shortcut Definitions In The Repo

| Shortcut | Scope | Target | Action | Module |
|---|---|---|---|---|
| F4 | Global | Alternate dashboard window | Dispatch grab based on current visit type | emr_assist/dashboard.py |
| Ctrl+Shift+G | Global | Alternate PyQt main window | Trigger appropriate grab | emr_assist/ui/main_window.py |
| Ctrl+Shift+H | Global | Alternate PyQt main window | Toggle GUI visibility | emr_assist/ui/main_window.py |
| Ctrl+Shift+N | Global | Alternate PyQt main window | Quick Next Task | emr_assist/ui/main_window.py |
| Ctrl+Shift+1 | Tab-sensitive | Alternate PyQt Hair tab | Insert hair note | emr_assist/ui/main_window.py |
| Ctrl+Shift+2 | Tab-sensitive | Alternate PyQt Hair tab | Insert hair limited check-in note | emr_assist/ui/main_window.py |
| Ctrl+Shift+3 | Tab-sensitive | Alternate PyQt Hair tab | Insert hair info | emr_assist/ui/main_window.py |
| Ctrl+Shift+1 | Tab-sensitive | Alternate PyQt Sexual Health tab | Insert Sexual Health note | emr_assist/ui/main_window.py |
| Ctrl+Shift+2 | Tab-sensitive | Alternate PyQt Sexual Health tab | Insert Sexual Health brief template | emr_assist/ui/main_window.py |
| Ctrl+Shift+3 | Tab-sensitive | Alternate PyQt Sexual Health tab | Insert Sexual Health change template | emr_assist/ui/main_window.py |
| Ctrl+Shift+4 | Tab-sensitive | Alternate PyQt Sexual Health tab | Insert SH change cadence | emr_assist/ui/main_window.py |
| Ctrl+Shift+5 | Tab-sensitive | Alternate PyQt Sexual Health tab | Insert SH change number | emr_assist/ui/main_window.py |
| Ctrl+Shift+6 | Tab-sensitive | Alternate PyQt Sexual Health tab | Insert SH change medication | emr_assist/ui/main_window.py |
| Ctrl+Shift+1 | Tab-sensitive | Alternate PyQt Birth Control tab | Insert Birth Control initial note | emr_assist/ui/main_window.py |
| Ctrl+Shift+2 | Tab-sensitive | Alternate PyQt Birth Control tab | Insert Birth Control follow-up note | emr_assist/ui/main_window.py |
| Ctrl+Return | Window-local | AI generation window | Generate all outputs | emr_assist/ai/window.py |
| Ctrl+1 | Window-local | AI generation window | Generate output 1 | emr_assist/ai/window.py |
| Ctrl+2 | Window-local | AI generation window | Generate output 2 | emr_assist/ai/window.py |
| Escape | Window-local | AI generation window | Stop generation | emr_assist/ai/window.py |
| Ctrl+Return | Window-local | Voice AI window | Generate | emr_assist/ai/voice_window.py |
| Escape | Window-local | Voice AI window | Stop | emr_assist/ai/voice_window.py |

## Notes

| Item | Detail |
|---|---|
| Simulated keystrokes excluded | Internal automation keystrokes like Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+Home, and Ctrl+End are not listed as user-facing shortcuts |
| Main area of concern | The clearest real conflict in the current wx app is Ctrl+Alt+H because it is bound to two different targets |
| Context-sensitive keys | Ctrl+Alt+F, Ctrl+Alt+N, and Ctrl+Alt+C are intentionally reused depending on active tab or overlay mode |
| Disabled systems | The alternate PyQt window, alternate dashboard, and AI Assist windows no longer register their shortcut sets |
