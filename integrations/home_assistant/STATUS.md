# STATUS: Scaffolded, not yet implemented

This folder is empty scaffolding for a future Home Assistant integration
(smart-home device control/status). No code exists here yet — no client,
no auth, no agent tool bindings.

**Intended scope (not yet designed in detail):**
- A Home Assistant REST/WebSocket API client
- Tools exposed via `TOOL_REGISTRY` (e.g. `ha_set_light`, `ha_get_state`)
  routed through the same `ACTION_GUARD` gating as other action types,
  not as a direct bypass
- Long-lived access token storage — coordinate with `security_future/secrets/`
  if/when that's built, or use `.env`-based secrets in the interim

Do not assume any of the above exists. This is a placeholder so the
directory's intent is documented instead of being a bare empty folder.

(Phase 4.5)
