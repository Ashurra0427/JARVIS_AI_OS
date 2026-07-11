# archive/legacy_action_layer — An EventBus-routed action layer that was never adopted

Archived 2026-06-20 during Phase 4.1 cleanup (move, not delete — all four
files are fully preserved below and still in version control).

## The shared story

These four files were designed together as one architecture: agents
publish `action.*.request` events on the EventBus, `ActionCoordinator`
subscribes and routes each request to the right manager (Browser, Desktop,
File, Terminal, API/Media), the manager executes and emits a result event,
and `ActionCoordinator` wraps that into `action.completed` /
`action.failed`. `ActionCoordinator`'s own docstring states the rule
explicitly: *"Agents NEVER call managers directly. ActionCoordinator is the
ONLY bridge between agents and managers."*

That rule was never actually enforced, because **nothing ever constructs
an `ActionCoordinator`**. The live system took a different, more direct
path instead: `server.py`'s `dispatch_tool()` / `ToolRegistry.invoke()`
call tool implementations directly, gated by `actions/security/` (
`PolicyEngine` → `PermissionManager` → `ActionGuard`) as a checkpoint
inside that direct call path — not by routing through a separate
EventBus-subscribed coordinator hub. `boot/bootstrap.py` even registers
no-op stub handlers for `actions.desktop` / `actions.browser` /
`actions.filesystem` with a comment ("these stubs satisfy the EventRouter
target resolution") that exists *only* to satisfy wiring for a coordinator
that was never built — those stubs are themselves a symptom of this same
unfinished migration, left in place as-is since 4.1's scope is these four
files specifically.

**Zero imports anywhere in the live codebase** for all four — confirmed by
grep across the full repo (module-path imports, bare-name references, and
the bootstrap.py comment above checked specifically — it names
`ActionCoordinator` but never imports or instantiates it).

## Per-file detail

### `action_coordinator.py` (590 lines)
The hub described above. Takes `browser_manager`, `api_manager`, and
implicitly desktop/file/terminal managers as constructor dependencies —
the other three files below are exactly the managers it was built to
route to. **Superseded by:** direct `ToolRegistry.invoke()` +
`actions/security/security_integration.py` as the real dispatch + policy
checkpoint.

### `browser_manager.py` (426 lines)
Session lifecycle (open/close/pool) + permission checks + routes to
`PlaywrightEngine`, reporting back via `action.browser.result` events.
**Superseded by:** `tools/browser_tools/browser_tools.py` — confirmed
canonical per the Phase 0.2 decision already recorded in `server.py`
(`_dispatch_tool_impl`'s `browser_*` branch imports `tools.browser_tools`
and gates every call through `STATE.action_guard.evaluate()` directly).
**Note:** only this one file is archived — `actions/browser/` is NOT
emptied. `actions/browser/playwright_engine.py` is still live and still
used directly by the canonical `tools/browser_tools/browser_tools.py` —
it simply has a different (and now sole) caller. `actions/browser/
browser_actions.py` also currently has zero importers, same as the four
files here, but it wasn't in Phase 4.1's named file list, so it's left
in place untouched rather than archived speculatively — flag for a future
pass if it's confirmed dead independently.

### `api_manager.py` (316 lines)
Generic external API execution: looks up endpoint configs from
`APIRegistry`, delegates HTTP execution to `APIExecutor`, publishes
`api.request.*` events. **Superseded by: nothing — no live replacement
exists.** `tools/web_tools/web_tools.py` covers web search/scrape/extract/
download, which is a different, narrower capability (no registered
endpoint configs, no generic authenticated-API abstraction). If
config-driven external API integration becomes a real requirement, this
is the design to revisit. Its three sibling files —
`actions/api/api_registry.py`, `api_executor.py`, `api_events.py` — are
**not** archived here (not in Phase 4.1's named list, and not
independently re-confirmed dead in this pass); they currently have no
other importer either now that `api_manager.py` is gone, so they're
effectively orphaned too, just out of this specific phase's scope. Flag
for Phase 4 follow-up or Phase 11.

### `media_service.py` (698 lines)
Windows 11 media control (play/pause/volume/mute/track nav) via pycaw →
winsdk → keyboard-fallback in that preference order. **Superseded by:
nothing — no live replacement exists.** There is currently no media-control
tool registered anywhere in `tools/`. This was a built-but-unconnected
capability, same situation as `api_manager.py` above.

## Why not deleted

Per project policy, no code is deleted. All four original files are
preserved unmodified in this folder.
