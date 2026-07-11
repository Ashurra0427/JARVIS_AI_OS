# Legacy Server Backends — Archived

Archived on 2026-06-13 during Phase 1 cleanup.

These 3 files (servers/server.py, servers/server1.py, servers/server3.py) were
near-duplicate forks of the same FastAPI+WebSocket "J.A.R.V.I.S HTML HUD" backend
(Groq -> Qwen-OpenVINO -> Gemini -> Ollama qwen2.5:1.5b fallback chain, port 7788,
serves webpage/jarvis.html).

**Canonical version kept: /server.py** (root-level), which:
- Was the most recently modified of the four
- Already contains the Ollama-timeout / OpenVINO-disable fixes
  (OLLAMA_MODEL=qwen2.5:1.5b, qwen_local_enabled default False)
- Is the simplest/most direct entrypoint path

No other file in the codebase imports these by module path (they are run
directly as scripts), so removing them from servers/ does not break any imports.
