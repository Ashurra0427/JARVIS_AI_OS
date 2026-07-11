# STATUS: Scaffolded, not yet implemented

This folder is empty scaffolding for a future Google services integration
(e.g. Calendar, Gmail, Drive, or Tasks). No code exists here yet — no
client, no auth flow, no agent tool bindings.

**Intended scope (not yet designed in detail):**
- OAuth2 flow for Google account linking
- Wrappers exposed as tools via `TOOL_REGISTRY` (e.g. `google_calendar_list`,
  `google_drive_search`) so agents can call them through the normal
  `ACTION_GUARD`-gated path, not as a bypass
- Credential storage — coordinate with `security_future/secrets/` if/when
  that's built, or use `.env`-based secrets in the interim

Do not assume any of the above exists. This is a placeholder so the
directory's intent is documented instead of being a bare empty folder.

(Phase 4.5)
