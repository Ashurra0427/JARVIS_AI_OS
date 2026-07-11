# actions/api — archived (this pass)

## What this module is

A generic "call an external REST API as an agent action" layer:
`api_registry.py` (per-service auth/base-url config), `api_executor.py`
(retry/backoff HTTP execution), `api_actions.py` (the ToolRegistry-style
action wrapper), `api_events.py` (`api.request.*` event constants — a
subset duplicated from the canonical `actions/action_events.py`).

`api_registry.register_defaults()` pre-registers: `openai`, `anthropic`,
`github`, `serper`, `elevenlabs`, `open_meteo`, `wolfram`, `newsapi`.

**This is not the LLM provider layer.** Chat/completions routing for
Groq, Gemini, etc. lives in `models/providers/` + `models/router/` and is
untouched by this archive — archiving `actions/api/` has no effect on
which cloud LLMs JARVIS talks to.

## Why it was archived

Confirmed via full-repo import-graph scan (2026-07-09):

- `actions/action_coordinator.py` accepts an `api_manager` constructor
  argument and has a working `_dispatch_api()` path, but **nothing ever
  passes an `api_manager` instance in** — `server.py`'s ActionCoordinator
  construction (Phase 8.1) leaves it at the default `None`. So even
  without archiving, `_dispatch_api()` was already a permanent no-op
  (`self._no_manager("api", request)`).
- Not registered with `tools/registry/` — no `api.*` tools exist.
- Not referenced in `config/tools.yaml`.
- The only other code in the whole repo that imported this package was
  `archive/legacy_action_layer/api_manager.py` — itself already archived
  before this pass, from an earlier EventBus-only action architecture
  that (per `action_coordinator.py`'s own header comment) "was never
  built."

In short: fully written, never wired, and not needed by the current
Groq + Groq-Whisper + Gemini cloud-LLM setup. Moved here rather than
deleted in case generic outbound REST-API actions (GitHub, weather,
Wolfram, etc.) become useful later — `api_manager.py` next to this
folder already shows how to re-wire it into `ActionCoordinator` via the
`api_manager=` constructor argument if that day comes.

## To bring it back

1. Move `api_actions.py`, `api_events.py`, `api_executor.py`,
   `api_registry.py` back to `actions/api/`.
2. Build an `APIManager` (see `../api_manager.py` for the old shape, or
   write a thinner one — `ActionCoordinator._dispatch_api()` only calls
   `api_manager.call(api_name, method, path, body, headers, query,
   requester, request_id)`).
3. Pass `api_manager=<instance>` into the `ActionCoordinator(...)` call
   in `server.py` (Phase 8.1 block).
4. Add real API keys for whichever of `openai` / `github` / `serper` /
   `elevenlabs` / `open_meteo` / `wolfram` / `newsapi` you want to `.env`
   / `config/settings.py`.
