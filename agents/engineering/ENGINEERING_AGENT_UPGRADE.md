# VISION — Engineering Agent Upgrade (v2)

**File:** `agents/engineering/engineering_agent.py`  
**Agent:** VISION (Agent 03)  
**Previous size:** ~23 KB (469 lines)  
**New size:** ~24 KB (531 lines)

---

## What changed and why (v1 → v2)

The v1 rewrite (Phase 8) was a major improvement — it replaced a fake 8-step pipeline with a real bounded action loop. The loop itself is solid and is **kept intact**. This upgrade adds targeted improvements on top of it without breaking anything.

---

## 1. Smarter context gathering

**v1 problem:** `_gather_real_context()` only looked for file paths mentioned explicitly in the goal text using a regex. A task like *"fix the auth bug"* contains no file paths, so v1 fell through to a bare directory listing and had no real content to work with.

**v2 fix:** The function now runs in three stages:

1. **`file.search`** — extracts 2 key terms from the description and searches the repo for matching filenames. A goal of *"fix the login timeout bug"* will search for `*login*` and `*timeout*` files.
2. **Explicit paths** — still reads any paths mentioned in the goal text or `context.files` (v1 behavior preserved).
3. **Directory listing** — only falls through to this if both above stages find nothing.

Each stage is independently failable — a `file.search` error doesn't abort the whole context step.

```python
# v1: only explicit paths
named_paths = re.findall(r"[\w\-./]+\.\w{1,5}", description)

# v2: search first, then explicit paths, then listing
for term in search_terms[:2]:
    result = await self._tool_registry.invoke("file.search", pattern=f"*{term}*", root=".")
    ...
```

---

## 2. Structured JSON action schema

**v1 problem:** The plan prompt requested `TOOL: / ARGS: / REASON: / DONE:` lines. Many capable models add preamble ("Sure, here's my next action:") or postamble ("Let me know if this works!") around these lines, causing `re.search` to fail or return garbage.

**v2 fix:** The plan prompt now requests a strict JSON block inside triple backticks:

```
```json
{
  "tool": "file.write",
  "args": {"path": "auth.py", "content": "..."},
  "reason": "Fix the timeout by...",
  "done": false
}
```
```

A new `_parse_action()` method tries JSON first, then falls back to the v1 regex parser for weak models that ignore the JSON instruction. This is the same parse-with-fallback pattern used elsewhere in the codebase.

**Tested cases:**
- ✅ JSON block with backticks (primary)
- ✅ `TOOL:/ARGS:/REASON:/DONE:` lines (weak model fallback)
- ✅ `done: true` detection
- ✅ Unparseable output → empty tool name (triggers stuck-reason path)

---

## 3. Completion gate — no victory without validation

**v1 problem:** The model could emit `DONE: yes` without ever having run a test or executed the code. The loop would accept this and declare success.

**v2 fix:** `succeeded = True` is only set when:
- The model emits `"done": true`, **AND**
- At least one `code.test` / `code.run_python` / `code.lint` action returned `success: true` OR the task was read-only (no `file.write` was executed)

If the model says done but no validation ran and files were touched, the loop **auto-injects a `code.test` step** on the first touched file before accepting completion:

```python
if done and not validation_ran and files_touched:
    tool_name = "code.test"
    args = {"path": files_touched[0]}
    reason = "Auto-injected: validate before completion"
    done = False
```

---

## 4. Test-first mode

When the goal contains any of: `test`, `tdd`, `spec`, `pytest`, the plan prompt includes an additional instruction:

```
TEST-FIRST MODE: Write the test file before the implementation.
Run tests after writing implementation to confirm they pass.
```

This is a prompt-level nudge, not hard-wired step ordering — the model still decides the actual steps, but it's told the expected order.

---

## 5. Partial output in return dict

**v1:** If the loop stopped before completion, the caller got `succeeded: false` but no content.

**v2:** A `partial_output` field contains the last successful `file.write` content (up to 500 chars), so CoordinatorAgent can show something useful even when the task didn't fully complete.

```python
return {
    "output": report,
    "description": description,
    "steps_taken": step_num,
    "succeeded": succeeded,
    "files_touched": files_touched,
    "capability_tier": tier,
    # new:
    "partial_output": partial_output,
    "test_passed": validation_ran and succeeded,
}
```

---

## Capability-aware behavior (unchanged from v1, limits raised)

| | Capable tier | Weak tier |
|---|---|---|
| Max steps | 10 (was 8) | 5 (unchanged) |
| Max retries/step | 3 (was 2) | 1 (unchanged) |
| Context size passed | 2500 chars/file | same |
| Fail fast on error | no | yes |

The tier is re-derived on every model call — if the router fails over mid-task, the loop adjusts immediately.

---

## New capabilities registered

| Capability | Tags |
|---|---|
| `code` | code, implement, develop, write, create |
| `debug` | debug, fix, troubleshoot, error, broken |
| `architecture` | architect, design, structure, plan |
| `test` | test, qa, verify, validate, tdd, spec |
| `review` | review, refactor, optimize, clean, improve |
| `analyze` | analyze, inspect, audit, read, understand |
| `patch` | patch, update, edit, change, modify |

---

## What was NOT changed

- The `UNDERSTAND → PLAN ONE STEP → ACT → OBSERVE → DECIDE` loop structure
- `_execute_action()` — same tool invocation and ACTION_GUARD handling
- `_broadcast_step()` — same event schema
- `_run_goal()` fallback-log behavior (BaseAgent handles this, not touched)
- Return dict keys from v1 (additive only)
- `__init__` signature

---

## Backups

Original v1 preserved at: `agents/engineering/engineering_agent.py.bak`
