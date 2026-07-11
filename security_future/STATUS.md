# STATUS: Unimplemented scaffolding — not the active security module

**This folder is empty scaffolding.** It contains no implementation. It is
**not** wired into any agent, tool call, or request path.

## Why this folder was renamed (Phase 4.4)

This directory used to be named `security/`. The codebase also has a
second, completely different directory called `actions/security/`, which
**is** the real, live security implementation:

```
actions/security/
├── policy_engine.py          ← real PolicyEngine
├── permission_manager.py     ← real PermissionManager
├── action_guard.py           ← real ActionGuard (gates filesystem/terminal/browser actions)
└── security_integration.py   ← real wiring into ToolRegistry
```

Having two same-named `security/` trees in one repo — one real, one empty —
is a search hazard: anyone grepping for "the security module" could easily
land in the empty one and assume the system has no working policy/permission
layer, or worse, start building duplicate logic here instead of extending
the real implementation.

This folder was renamed from `security/` to `security_future/` so it can
never be mistaken for `actions/security/` again. No files were deleted —
every subfolder below is preserved exactly as it was.

## If you are looking for the real security/permission/sandbox logic

Go to `actions/security/` instead. That is the module that `ToolRegistry`
and `server.py`'s `ACTION_GUARD` actually call on every filesystem, terminal,
and browser action.

## Subfolders in this scaffold and their intended (unbuilt) scope

| Folder | Intended future scope |
|---|---|
| `permissions/` | A possible future permissions model distinct from `actions/security/permission_manager.py` — scope not yet decided. Do not build here without first deciding whether this supersedes or complements the existing `PermissionManager`. |
| `sandbox/` | A possible future OS-level sandboxing layer (e.g. containerized or namespaced execution) for terminal/command actions, beyond the current `command_validator.py` allow/deny-list approach. |
| `audit/` | A possible future structured audit-log store for security-relevant events, distinct from the general app logger currently in use. |
| `secrets/` | A possible future secrets manager (e.g. for `JARVIS_SECRET`, API keys) beyond plain `.env` files. |

None of the above have been designed in detail. Treat every subfolder here
as "name reserved, not implemented" — do not assume any behavior exists
just because the folder exists.

## Status

**Scaffolded, not yet implemented.** Folders kept in the repo intentionally
per the project's "archive, never delete" policy. (Phase 4.4)
