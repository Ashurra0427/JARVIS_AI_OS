# ATHENA — Research Agent Upgrade (v2)

**File:** `agents/research/research_agent.py`  
**Agent:** ATHENA (Agent 02)  
**Previous size:** 4.7 KB (103 lines)  
**New size:** ~17 KB (368 lines)

---

## What was wrong with v1

The original agent was a single LLM call wrapped in optional web search. It had:

- No planning — it sent the raw goal directly to the model
- No multi-source triangulation — one `web.search` call, then one synthesis call
- No structure — output was whatever the model returned, no sections, no confidence ratings
- No fact-checking — claims were never verified
- No phase visibility — the UI had no idea what stage the agent was in

In short, it was a chatbot wearing a research agent's name tag.

---

## What's new in v2

### Phase 1 — Decompose
The agent now asks the model to split the research goal into **2–5 focused sub-questions** before searching anything. This prevents the common failure where a broad query like "best practices for microservices security" returns generic snippets that don't actually answer what was asked.

```
Goal: "Compare gRPC vs REST for a high-throughput IoT backend"
  → Sub-question 1: "gRPC performance benchmarks IoT high throughput"
  → Sub-question 2: "REST API limitations real-time IoT data"
  → Sub-question 3: "gRPC vs REST latency comparison embedded systems"
```

### Phase 2 — Search per sub-question
Each sub-question gets its own `web.search` call via the ToolRegistry. Results are collected as numbered source blocks with title + snippet, not merged into an undifferentiated blob.

Degrades gracefully: if `web.search` fails for a sub-question, the agent falls back to asking the model what it already knows about that specific sub-question.

### Phase 3 — Deep-read (capable tier only)
On a capable cloud model, the agent fetches the top URL from the first search using `web.fetch` for a deeper one-level read. This gives the synthesis model actual article content rather than just a snippet.

Skipped on weak/local models to avoid burning tokens on a model that won't use the extra context well.

### Phase 4 — Structured synthesis
All evidence blocks are passed to one synthesis call with a strict prompt requesting:

- `## Key Findings` — bullet points with `[N]` source citations
- `## Analysis` — cross-source synthesis, agreements and conflicts noted
- `## Confidence Assessment` — high/medium/low per major claim with reasons
- `## Knowledge Gaps` — what is still uncertain or missing
- `## Sources` — numbered source list

### Phase 5 — Fact-check (capable tier only)
After synthesis, the agent asks the model: *"What is the single most uncertain claim worth verifying?"* It runs one targeted verification search and appends a `## Verification Note` to the final output if it finds confirming or contradicting evidence.

### Phase broadcasting
Every phase emits `agent.workflow.step` events to the event bus, so the UI can show:
```
ATHENA: Decomposing research goal... ✅
ATHENA: Searching sub-question 1/3... ✅
ATHENA: Synthesising findings... ⏳
```

---

## Capability-aware behavior

| Feature | Capable tier (groq/gemini) | Weak tier (ollama/local) |
|---|---|---|
| Sub-questions | Up to 5 | Up to 2 |
| Deep-read (web.fetch) | ✅ | ❌ skipped |
| Fact-check loop | ✅ | ❌ skipped |
| Synthesis prompt | Full structured | Condensed |
| max_tokens synthesis | 1500 | 600 |

The tier is detected on the first model call via `complete_with_provider()`. The agent still returns a useful result on weak/local hardware — it just has fewer sources and no verification pass.

---

## New capabilities registered

| Capability | Tags |
|---|---|
| `search` | search, web, lookup |
| `research` | research, investigate, find |
| `summarize` | summarize, tldr, summary |
| `factcheck` | verify, factcheck, check |
| `compare` | compare, vs, difference |
| `analyze` | analyze, analyse, trends |

---

## New metrics emitted

| Metric | Description |
|---|---|
| `sources_scanned` | Total search results returned |
| `searches_run` | Number of `web.search` calls made |
| `new_findings` | Snippet lines collected |
| `synthesis_calls` | Number of synthesis LLM calls |
| `accuracy_pct` | 96 (capable) / 88 (weak) |
| `current_phase` | Live phase label for dashboard |

---

## Backward compatibility

The return dict adds new keys but does not remove or rename existing ones:

```python
{
    "findings": str,          # was present in v1
    "description": str,       # was present in v1
    # new:
    "sub_questions": list,
    "sources_scanned": int,
    "capability_tier": str,
}
```

The `__init__` signature is unchanged — same parameters as v1.

---

## Tool dependencies

| Tool | Required? | Degradation if absent |
|---|---|---|
| `web.search` | Recommended | Falls back to model knowledge per sub-question |
| `web.fetch` | Optional | Deep-read phase skipped |

---

## Backups

Original v1 preserved at: `agents/research/research_agent.py.bak`
