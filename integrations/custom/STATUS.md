# STATUS: Scaffolded, not yet implemented

This folder is empty scaffolding intended for user-defined / third-party
custom integrations that don't fit the other named integration folders
(`google/`, `home_assistant/`, `github/`, `mobile/`). No code exists here yet.

**Intended scope (not yet designed in detail):**
- A pattern/interface for dropping in a custom integration module (e.g. a
  base class or registration hook similar to how `config/app_registry.py`
  or `config/tools.yaml` register other capabilities)
- Documentation for end users on how to add their own integration without
  modifying core `agents/` or `actions/` code

Do not assume any of the above exists. This is a placeholder so the
directory's intent is documented instead of being a bare empty folder.

(Phase 4.5)
