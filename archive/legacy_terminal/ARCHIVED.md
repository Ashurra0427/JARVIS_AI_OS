# actions/terminal/terminal_actions.py — archived (this pass)
==============================================================

## What this module is

`TerminalActions` is a low-level async subprocess executor (execute_command,
execute_powershell, execute_cmd, stream_output, cancel/kill) that bakes in a
defense-in-depth `SAFE_CMDS` allowlist (Phase 0.3 fix). It deliberately does
no other security validation.

## Why it was archived

Superseded by `actions/terminal/terminal_manager.py`, the production manager
that is actually wired into `ActionCoordinator`. `TerminalManager` performs
real validation through `command_validator.validate_command` (risk scoring),
execution through `command_executor.CommandExecutor`, session management,
events, and ServiceRegistry registration. A repo-wide import-graph scan
confirmed `TerminalActions` is imported nowhere in the live system (its
`SAFE_CMDS` allowlist behaviour is now covered by the risk-based
`CommandValidator`, which is independently tested in
`tests/test_phase1_security.py`). Moved here rather than deleted.

## To bring it back

1. Move `terminal_actions.py` back to `actions/terminal/`.
2. Wire it behind `TerminalManager` as the execution core, or fold its
   `SAFE_CMDS` allowlist into `command_validator` if you want that
   defense-in-depth list back alongside the risk scorer.
